"""Microsoft Purview Privacy - Subject Rights Requests (GDPR / DSR).

Subject Rights Requests (``/security/subjectRightsRequests``) track an
organization's responses to data-subject privacy requests (access, export,
delete, tag-for-action) under regulations such as GDPR and CCPA. Microsoft
Graph only exposes this API to **delegated** (signed-in user) credentials, so
the tools short-circuit with an actionable message in app-only mode.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import DELEGATED_ONLY_HINT, PurviewClient, dt, fmt_list, odata_quote, trunc

SRC = "integration:purview"
_SRR = "/security/subjectRightsRequests"


def _fmt_srr(r: dict[str, Any]) -> str:
    subject = r.get("dataSubject") or {}
    name = (
        f"{subject.get('firstName', '')} {subject.get('lastName', '')}".strip()
        or subject.get("email")
        or "?"
    )
    return (
        f"- {r.get('displayName', 'Request')} | type: {r.get('type', '?')} | "
        f"status: {r.get('status', '?')}\n"
        f"  id: {r.get('id', '?')} | subject: {name} <{subject.get('email', '?')}> | "
        f"due: {dt(r.get('internalDueDateTime'))}\n"
        f"  created: {dt(r.get('createdDateTime'))} | "
        f"{trunc(r.get('description'), 120)}"
    )


def register(
    client: PurviewClient, executor: CallableToolExecutor
) -> list[ToolDefinition]:
    def _guard() -> str | None:
        return DELEGATED_ONLY_HINT if client.app_only else None

    async def list_purview_subject_rights_requests(
        status: str = "", type: str = "", top: int = 25, skiptoken: str = ""
    ) -> str:
        if guard := _guard():
            return guard
        filters: list[str] = []
        if status:
            filters.append(f"status eq '{odata_quote(status)}'")
        if type:
            filters.append(f"type eq '{odata_quote(type)}'")
        params: dict[str, Any] = {}
        if filters:
            params["$filter"] = " and ".join(filters)
        if skiptoken:
            params["$skiptoken"] = skiptoken
        items, err, next_tok = await client.graph_paged(
            _SRR, "list subject rights requests", top=min(top, 100), params=params
        )
        if err:
            return err
        return fmt_list(
            items,
            _fmt_srr,
            noun="subject rights request",
            skiptoken=next_tok,
            empty_msg="No subject rights requests found.",
        )

    async def get_purview_subject_rights_request(request_id: str) -> str:
        if guard := _guard():
            return guard
        data, err = await client.graph_get_json(
            f"{_SRR}/{quote(request_id.strip(), safe='')}",
            "get subject rights request",
        )
        if err:
            return err
        return _fmt_srr(data or {})

    executor.register(
        "list_purview_subject_rights_requests", list_purview_subject_rights_requests
    )
    executor.register(
        "get_purview_subject_rights_request", get_purview_subject_rights_request
    )

    return [
        ToolDefinition(
            name="list_purview_subject_rights_requests",
            description=(
                "List Microsoft Purview Subject Rights Requests (privacy / GDPR / "
                "DSR) - data-subject requests for access, export, deletion, or "
                "tagging. Optionally filter by status (active/closed) and type "
                "(export/delete/access/tagForAction). Returns each request's "
                "subject, type, status, and internal due date. Requires delegated "
                "(signed-in) access."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "description": "Filter by status, e.g. 'active' or 'closed'.",
                    },
                    "type": {
                        "type": "string",
                        "description": "Filter by request type: export, delete, access, tagForAction.",
                    },
                    "top": {"type": "integer", "default": 25},
                    "skiptoken": {"type": "string"},
                },
            },
            source=SRC,
            priority=0,
        ),
        ToolDefinition(
            name="get_purview_subject_rights_request",
            description=(
                "Get one Subject Rights Request by id, including the data subject, "
                "type, status, stages, and due date. Use "
                "list_purview_subject_rights_requests first to find the id. "
                "Requires delegated access."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "request_id": {
                        "type": "string",
                        "description": "The subject rights request id.",
                    }
                },
                "required": ["request_id"],
            },
            source=SRC,
            priority=0,
        ),
    ]
