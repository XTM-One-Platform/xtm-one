"""Drata audits tools - Audits."""

from __future__ import annotations

from typing import Any

from app.core.egress import egress_client
from app.integrations.http import format_entity_list
from app.tools.base import CallableToolExecutor, ToolDefinition

from . import _common
from ._common import (
    _PROVIDER,
    _full_name,
)


def register(
    base_url: str,
    headers: dict[str, str],
    upload_headers: dict[str, str],
    executor: CallableToolExecutor,
) -> list[ToolDefinition]:
    """Build the Drata audits tools and register their callables."""

    async def list_drata_audits(workspace_id: int = 1, status: str = "") -> str:
        """List compliance audits in a workspace. Optionally filter by status."""
        async with egress_client(timeout=30) as client:
            params: dict[str, Any] = {"expand[]": ["auditors", "internalAuditors"]}
            if status:
                params["status"] = status
            items, err = await _common._paginated_fetch(
                client,
                f"{base_url}/workspaces/{workspace_id}/audits",
                headers,
                params,
            )
            if err:
                return err
            return format_entity_list(
                items,
                lambda a: (
                    f"[{a.get('id')}] Framework: {a.get('frameworkType', 'N/A')} | "
                    f"Type: {a.get('auditType', 'N/A')} | "
                    f"Internal: {a.get('isInternalAudit', False)} | "
                    f"Status: {a.get('status', 'N/A')} | "
                    f"Start: {a.get('startDate', 'N/A')} | "
                    f"End: {a.get('endDate', 'N/A')}"
                ),
                noun="audit",
            )

    async def get_drata_audit(audit_id: str, workspace_id: int = 1) -> str:
        """Get details of a specific audit including auditors and linked controls."""
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client,
                "get",
                f"{base_url}/workspaces/{workspace_id}/audits/{audit_id}",
                _PROVIDER,
                "get audit",
                headers=headers,
                params={"expand[]": ["auditors", "internalAuditors", "controls"]},
            )
            if err:
                return err
            a = resp.json()
            lines = [
                f"Audit (ID: {a.get('id')})",
                f"Framework: {a.get('frameworkType', 'N/A')}",
                f"Type: {a.get('auditType', 'N/A')}",
                f"Internal: {a.get('isInternalAudit', False)}",
                f"Status: {a.get('status', 'N/A')}",
                f"Start: {a.get('startDate', 'N/A')}",
                f"End: {a.get('endDate', 'N/A')}",
                f"Completed: {a.get('completedAt', 'N/A')}",
            ]
            auditors = a.get("auditors", []) or []
            if auditors:
                lines.append(f"\nExternal Auditors ({len(auditors)}):")
                for x in auditors:
                    lines.append(f"  {x.get('name', '?')} <{x.get('email', 'N/A')}>")
            internal = a.get("internalAuditors", []) or []
            if internal:
                lines.append(f"\nInternal Auditors ({len(internal)}):")
                for x in internal:
                    lines.append(f"  {_full_name(x)} <{x.get('email', 'N/A')}>")
            controls = a.get("controls", []) or []
            if controls:
                lines.append(f"\nControls ({len(controls)}):")
                for c in controls:
                    lines.append(f"  [{c.get('code', '?')}] {c.get('name', 'Untitled')}")
            return "\n".join(lines)

    callable_map = {
        "list_drata_audits": list_drata_audits,
        "get_drata_audit": get_drata_audit,
    }
    for _name, _fn in callable_map.items():
        executor.register(_name, _fn)

    return [
        ToolDefinition(
            name="list_drata_audits",
            description="List compliance audits in a Drata workspace. Optionally filter by status.",
            input_schema={
                "type": "object",
                "properties": {
                    "workspace_id": {"type": "integer", "default": 1},
                    "status": {"type": "string", "description": "Filter by audit status"},
                },
                "required": [],
            },
            source="integration:drata",
            priority=1,
        ),
        ToolDefinition(
            name="get_drata_audit",
            description="Get details of a specific audit including auditors and linked controls.",
            input_schema={
                "type": "object",
                "properties": {
                    "workspace_id": {"type": "integer", "default": 1},
                    "audit_id": {"type": "string", "description": "Audit UUID"},
                },
                "required": ["audit_id"],
            },
            source="integration:drata",
            priority=1,
        ),
    ]
