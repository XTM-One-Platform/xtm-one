"""Drata frameworks tools - Compliance frameworks and their requirements."""

from __future__ import annotations

from app.core.egress import egress_client
from app.integrations.http import format_entity_list
from app.tools.base import CallableToolExecutor, ToolDefinition

from . import _common
from ._common import (
    _ws_only,
)


def register(
    base_url: str,
    headers: dict[str, str],
    upload_headers: dict[str, str],
    executor: CallableToolExecutor,
) -> list[ToolDefinition]:
    """Build the Drata frameworks tools and register their callables."""

    async def list_drata_frameworks(workspace_id: int = 1) -> str:
        """List compliance frameworks active in a Drata workspace."""
        async with egress_client(timeout=30) as client:
            items, err = await _common._paginated_fetch(
                client,
                f"{base_url}/workspaces/{workspace_id}/frameworks",
                headers,
                {},
            )
            if err:
                return err
            return format_entity_list(
                items,
                lambda f: (
                    f"[{f.get('id')}] {f.get('name', 'Unnamed')} "
                    f"(slug: {f.get('slug', 'N/A')}) — "
                    f"Ready: {f.get('isReady', 'N/A')} | "
                    f"Requirements: {f.get('numReadyInScopeRequirements', '?')}/"
                    f"{f.get('numInScopeRequirements', '?')} | "
                    f"Audit Type: {f.get('auditType', 'N/A')}"
                ),
                noun="framework",
            )

    async def list_drata_framework_requirements(framework_id: int, workspace_id: int = 1) -> str:
        """List requirements for a specific compliance framework."""
        async with egress_client(timeout=30) as client:
            items, err = await _common._paginated_fetch(
                client,
                f"{base_url}/workspaces/{workspace_id}/frameworks/{framework_id}/requirements",
                headers,
                {},
            )
            if err:
                return err
            return format_entity_list(
                items,
                lambda r: (
                    f"[{r.get('name', '?')}] {(r.get('description') or '')[:140]} — "
                    f"In Scope: {r.get('isInScope', 'N/A')} | "
                    f"Ready: {r.get('isReady', 'N/A')}"
                ),
                noun="requirement",
            )

    callable_map = {
        "list_drata_frameworks": list_drata_frameworks,
        "list_drata_framework_requirements": list_drata_framework_requirements,
    }
    for _name, _fn in callable_map.items():
        executor.register(_name, _fn)

    return [
        ToolDefinition(
            name="list_drata_frameworks",
            description="List compliance frameworks active in a Drata workspace, with readiness and requirement counts.",
            input_schema=_ws_only(),
            source="integration:drata",
            priority=1,
        ),
        ToolDefinition(
            name="list_drata_framework_requirements",
            description="List requirements for a specific compliance framework (e.g. SOC 2, ISO 27001).",
            input_schema={
                "type": "object",
                "properties": {
                    "workspace_id": {"type": "integer", "default": 1},
                    "framework_id": {"type": "integer"},
                },
                "required": ["framework_id"],
            },
            source="integration:drata",
            priority=2,
        ),
    ]
