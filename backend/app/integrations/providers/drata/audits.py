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

#: Request statuses Drata public API v2 accepts in ``status[]`` (``CustomerRequestStatus``).
AUDIT_REQUEST_STATUSES: tuple[str, ...] = ("OUTSTANDING", "IN_REVIEW", "ACCEPTED", "CHANGES_REQUESTED")

#: Characters of one message shown in a listing; ``get_drata_audit_request`` shows them whole.
_LIST_MESSAGE_MAX_CHARS = 300


def _owners_line(request: dict) -> str:
    owners = request.get("owners") or []
    if not owners:
        return "Owners: none"
    return "Owners: " + ", ".join(f"{_full_name(o)} <{o.get('email', 'N/A')}>" for o in owners)


def _message_lines(request: dict, max_chars: int | None = None) -> list[str]:
    messages = sorted(request.get("messages") or [], key=lambda m: str(m.get("createdAt") or ""))
    lines: list[str] = []
    for m in messages:
        text = " ".join(str(m.get("message") or "").split())
        if max_chars is not None and len(text) > max_chars:
            text = text[:max_chars] + "..."
        lines.append(f"  [{m.get('createdAt', 'N/A')}] {m.get('sentByEmail') or 'N/A'}: {text}")
    return lines


def _format_audit_request(request: dict, *, include_messages: bool) -> str:
    head = (
        f"[{request.get('id')}] {request.get('code') or '?'} | Status: {request.get('status', 'N/A')} | "
        f"{request.get('title') or 'Untitled'} | Updated: {request.get('updatedAt', 'N/A')}"
    )
    lines = [head, f"  {_owners_line(request)}"]
    if include_messages:
        messages = _message_lines(request, _LIST_MESSAGE_MAX_CHARS)
        lines.append(f"  Messages ({len(messages)}):" if messages else "  Messages: none")
        lines.extend("  " + line for line in messages)
    return "\n".join(lines)


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

    async def list_drata_audit_requests(
        audit_id: str,
        workspace_id: int = 1,
        status: list[str] | None = None,
        owner_ids: list[int] | None = None,
        include_messages: bool = False,
    ) -> str:
        """List the requests of an audit with their status and owners, optionally their messages."""
        statuses = [str(s).strip().upper() for s in (status or []) if str(s).strip()]
        unknown = [s for s in statuses if s not in AUDIT_REQUEST_STATUSES]
        if unknown:
            return f"Error: unknown audit request status {', '.join(unknown)}; use {', '.join(AUDIT_REQUEST_STATUSES)}."
        params: dict[str, Any] = {"expand[]": ["owners", "messages"] if include_messages else ["owners"]}
        if statuses:
            params["status[]"] = statuses
        if owner_ids:
            params["userIds[]"] = [int(u) for u in owner_ids]
        async with egress_client(timeout=30) as client:
            items, err, truncated = await _common._paginated_fetch_all(
                client,
                f"{base_url}/workspaces/{workspace_id}/audits/{audit_id}/requests",
                headers,
                params,
                action="list audit requests",
            )
        if err:
            return err
        if not items:
            return "No audit requests found."
        counts: dict[str, int] = {}
        for r in items:
            key = str(r.get("status") or "N/A")
            counts[key] = counts.get(key, 0) + 1
        summary = ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
        out = [f"Found {len(items)} audit request(s): {summary}"]
        out.extend(_format_audit_request(r, include_messages=include_messages) for r in items)
        if truncated:
            out.append(f"Warning: capped at {len(items)} requests; Drata has more. Narrow with status or owner_ids.")
        return "\n".join(out)

    async def get_drata_audit_request(audit_id: str, request_id: int, workspace_id: int = 1) -> str:
        """Get one audit request with its status, owners, every message and its mapped controls."""
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client,
                "get",
                f"{base_url}/workspaces/{workspace_id}/audits/{audit_id}/requests/{request_id}",
                _PROVIDER,
                "get audit request",
                headers=headers,
                params={"expand[]": ["owners", "messages", "controls"]},
            )
        if err:
            return err
        r = resp.json()
        lines = [
            f"Audit request {r.get('code') or '?'} (ID: {r.get('id')})",
            f"Title: {r.get('title') or 'Untitled'}",
            f"Status: {r.get('status', 'N/A')}",
            f"Audit: {r.get('auditId', audit_id)}",
            f"Created: {r.get('createdAt', 'N/A')} | Updated: {r.get('updatedAt', 'N/A')}",
            _owners_line(r),
        ]
        description = str(r.get("description") or "").strip()
        if description:
            lines.append(f"\nDescription:\n{description}")
        messages = _message_lines(r)
        lines.append(f"\nMessages ({len(messages)}):" if messages else "\nMessages: none")
        lines.extend(messages)
        controls = r.get("controls") or []
        if controls:
            lines.append(f"\nControls ({len(controls)}):")
            for c in controls:
                lines.append(f"  [{c.get('code', '?')}] {c.get('name', 'Untitled')}")
        return "\n".join(lines)

    callable_map = {
        "list_drata_audits": list_drata_audits,
        "get_drata_audit": get_drata_audit,
        "list_drata_audit_requests": list_drata_audit_requests,
        "get_drata_audit_request": get_drata_audit_request,
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
        ToolDefinition(
            name="list_drata_audit_requests",
            description=(
                "List an audit's evidence requests with status and owners, optionally the auditor messages. "
                "Drata's API cannot set a request status."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "audit_id": {"type": "string", "description": "Audit UUID (full id)"},
                    "workspace_id": {"type": "integer", "default": 1},
                    "status": {
                        "type": "array",
                        "items": {"type": "string", "enum": list(AUDIT_REQUEST_STATUSES)},
                        "description": "Only requests in these statuses",
                    },
                    "owner_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Only requests owned by these Drata user ids",
                    },
                    "include_messages": {
                        "type": "boolean",
                        "default": False,
                        "description": "Add each request's messages, cut to 300 characters",
                    },
                },
                "required": ["audit_id"],
            },
            source="integration:drata",
            priority=1,
        ),
        ToolDefinition(
            name="get_drata_audit_request",
            description="Get one audit request: status, owners, description, every auditor message and mapped controls.",
            input_schema={
                "type": "object",
                "properties": {
                    "audit_id": {"type": "string", "description": "Audit UUID (full id)"},
                    "request_id": {"type": "integer", "description": "Audit request id"},
                    "workspace_id": {"type": "integer", "default": 1},
                },
                "required": ["audit_id", "request_id"],
            },
            source="integration:drata",
            priority=1,
        ),
    ]
