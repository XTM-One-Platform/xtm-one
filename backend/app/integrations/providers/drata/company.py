"""Drata company tools - Company profile + workspaces."""

from __future__ import annotations

from app.core.egress import egress_client
from app.integrations.http import format_entity_list
from app.tools.base import CallableToolExecutor, ToolDefinition

from . import _common
from ._common import (
    _PROVIDER,
    _empty,
)


def register(
    base_url: str,
    headers: dict[str, str],
    upload_headers: dict[str, str],
    executor: CallableToolExecutor,
) -> list[ToolDefinition]:
    """Build the Drata company tools and register their callables."""

    async def get_drata_company() -> str:
        """Get the Drata company profile."""
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client,
                "get",
                f"{base_url}/company",
                _PROVIDER,
                "get company details",
                headers=headers,
            )
            if err:
                return err
            c = resp.json()
            lines = [
                f"Company: {c.get('name', 'N/A')}",
                f"Legal Name: {c.get('legalName', 'N/A')}",
                f"Domain: {c.get('domain', 'N/A')}",
                f"Founded: {c.get('year', 'N/A')}",
                f"Description: {c.get('description', 'N/A')}",
                f"Phone: {c.get('phoneNumber', 'N/A')}",
                f"Address: {c.get('address', 'N/A')}",
                f"Security Email: {c.get('securityEmail', 'N/A')}",
                f"Language: {c.get('language', 'N/A')}",
            ]
            sr = c.get("securityReport", {})
            if sr:
                lines.append(
                    f"Security Report: visibility={sr.get('visibility', 'N/A')}, sharing={sr.get('sharing', False)}"
                )
            return "\n".join(lines)

    async def list_drata_workspaces() -> str:
        """List Drata workspaces (needed for controls which are workspace-scoped)."""
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client,
                "get",
                f"{base_url}/workspaces",
                _PROVIDER,
                "list workspaces",
                headers=headers,
            )
            if err:
                return err
            data = resp.json()
            workspaces = data.get("data", data) if isinstance(data, dict) else data
            if isinstance(workspaces, dict):
                workspaces = [workspaces]
            return format_entity_list(
                workspaces,
                lambda w: f"ID: {w.get('id')} — {w.get('name', 'Unnamed')}",
                noun="workspace",
            )

    callable_map = {
        "get_drata_company": get_drata_company,
        "list_drata_workspaces": list_drata_workspaces,
    }
    for _name, _fn in callable_map.items():
        executor.register(_name, _fn)

    return [
        ToolDefinition(
            name="get_drata_company",
            description="Get the Drata company profile including domain, address, security settings.",
            input_schema=_empty,
            source="integration:drata",
            priority=2,
        ),
        ToolDefinition(
            name="list_drata_workspaces",
            description="List Drata workspaces. Workspace IDs are required for controls, evidence, audits, frameworks, and monitoring tests.",
            input_schema=_empty,
            source="integration:drata",
            priority=2,
        ),
    ]
