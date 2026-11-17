# SPDX-License-Identifier: Apache-2.0

"""OpenAEV finding + vulnerability tools (writes).

* ``manage_openaev_finding``       - finding CRUD (search is in the core
  search_openaev_findings tool).
* ``manage_openaev_vulnerability`` - vulnerability list/get/create/update/delete
  (CVE lookup is in the core get_openaev_vulnerability tool).
"""

from __future__ import annotations

import json
import logging

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import _api, _search_body

logger = logging.getLogger(__name__)

_SOURCE = "integration:openaev"


def register(
    base_url: str, headers: dict[str, str], executor: CallableToolExecutor
) -> list[ToolDefinition]:
    async def manage_openaev_finding(
        action: str = "",
        finding_id: str = "",
        field: str = "",
        finding_type: str = "",
        value: str = "",
        labels: list[str] | None = None,
        inject_id: str = "",
    ) -> str:
        action = (action or "").strip().lower()

        def _body() -> dict:
            b: dict = {}
            if field:
                b["finding_field"] = field
            if finding_type:
                b["finding_type"] = finding_type
            if value:
                b["finding_value"] = value
            if labels is not None:
                b["finding_labels"] = labels
            if inject_id:
                b["finding_inject_id"] = inject_id
            return b

        if action == "get":
            if not finding_id:
                return "finding_id is required for action 'get'."
            resp, err = await _api(
                base_url, headers, "get", f"/api/findings/{finding_id}", "get finding"
            )
            if err:
                return err
            return json.dumps(resp.json(), default=str)[:2000]

        if action in ("create", "update"):
            if action == "create" and not (field and finding_type and value):
                return "field, finding_type and value are required for action 'create'."
            if action == "update" and not finding_id:
                return "finding_id is required for action 'update'."
            body = _body()
            if action == "update" and not body:
                return "Provide at least one field to update."
            if action == "create":
                resp, err = await _api(
                    base_url, headers, "post", "/api/findings", "create finding",
                    json=body,
                )
            else:
                resp, err = await _api(
                    base_url, headers, "put", f"/api/findings/{finding_id}",
                    "update finding", json=body,
                )
            if err:
                return err
            f = resp.json()
            verb = "Created" if action == "create" else "Updated"
            return f"{verb} finding (id: {f.get('finding_id')})."

        if action == "delete":
            if not finding_id:
                return "finding_id is required for action 'delete'."
            _, err = await _api(
                base_url, headers, "delete", f"/api/findings/{finding_id}",
                "delete finding",
            )
            if err:
                return err
            return f"Deleted finding {finding_id}."

        return (
            f"Unknown action '{action}'. Valid actions: get, create, update, delete. "
            "(Use search_openaev_findings to search.)"
        )

    async def manage_openaev_vulnerability(
        action: str = "",
        vulnerability_id: str = "",
        external_id: str = "",
        cvss: float | None = None,
        description: str = "",
        config: str = "",
        search: str = "",
        limit: int = 20,
    ) -> str:
        action = (action or "").strip().lower()

        extra: dict = {}
        if config:
            try:
                extra = json.loads(config)
            except ValueError as exc:
                return f"config must be a valid JSON object: {exc}"
            if not isinstance(extra, dict):
                return "config must be a JSON object of field->value pairs."

        def _body() -> dict:
            b = dict(extra)
            if external_id:
                b["vulnerability_external_id"] = external_id
            if cvss is not None:
                b["vulnerability_cvss_v31"] = cvss
            if description:
                b["vulnerability_description"] = description
            return b

        if action == "list":
            resp, err = await _api(
                base_url, headers, "post", "/api/vulnerabilities/search",
                "search vulnerabilities",
                json=_search_body(size=min(limit, 50), text_search=search or None),
            )
            if err:
                return err
            data = resp.json()
            items = data.get("content", data) if isinstance(data, dict) else data
            if not items:
                return "No vulnerabilities found."
            return f"Vulnerabilities ({len(items)}):\n" + "\n".join(
                f"- {v.get('vulnerability_external_id', '?')} "
                f"(cvss {v.get('vulnerability_cvss_v31', '?')}) "
                f"(id: {v.get('vulnerability_id')})" for v in items
            )

        if action == "get":
            if not vulnerability_id:
                return "vulnerability_id is required for action 'get'."
            resp, err = await _api(
                base_url, headers, "get", f"/api/vulnerabilities/{vulnerability_id}",
                "get vulnerability",
            )
            if err:
                return err
            return json.dumps(resp.json(), default=str)[:2000]

        if action in ("create", "update"):
            if action == "create" and (not external_id or cvss is None):
                return "external_id (CVE) and cvss (0.0-10.0) are required for 'create'."
            if action == "update" and not vulnerability_id:
                return "vulnerability_id is required for action 'update'."
            body = _body()
            if action == "update" and not body:
                return "Provide at least one field to update (external_id, cvss, description, or config)."
            if action == "create":
                resp, err = await _api(
                    base_url, headers, "post", "/api/vulnerabilities",
                    "create vulnerability", json=body,
                )
            else:
                resp, err = await _api(
                    base_url, headers, "put", f"/api/vulnerabilities/{vulnerability_id}",
                    "update vulnerability", json=body,
                )
            if err:
                return err
            v = resp.json()
            verb = "Created" if action == "create" else "Updated"
            return f"{verb} vulnerability '{v.get('vulnerability_external_id')}' (id: {v.get('vulnerability_id')})."

        if action == "delete":
            if not vulnerability_id:
                return "vulnerability_id is required for action 'delete'."
            _, err = await _api(
                base_url, headers, "delete",
                f"/api/vulnerabilities/{vulnerability_id}", "delete vulnerability",
            )
            if err:
                return err
            return f"Deleted vulnerability {vulnerability_id}."

        return (
            f"Unknown action '{action}'. Valid actions: list, get, create, update, delete."
        )

    executor.register("manage_openaev_finding", manage_openaev_finding)
    executor.register("manage_openaev_vulnerability", manage_openaev_vulnerability)

    return [
        ToolDefinition(
            name="manage_openaev_finding",
            description=(
                "Manage OpenAEV findings. Actions: 'get' (finding_id), 'create' (field "
                "+ finding_type + value, optional labels, inject_id), 'update' "
                "(finding_id + fields), 'delete' (finding_id). Use "
                "search_openaev_findings to search."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["get", "create", "update", "delete"]},
                    "finding_id": {"type": "string"},
                    "field": {"type": "string", "description": "Finding field/key."},
                    "finding_type": {"type": "string", "description": "Contract output type (text, number, port, ...)."},
                    "value": {"type": "string"},
                    "labels": {"type": "array", "items": {"type": "string"}},
                    "inject_id": {"type": "string", "description": "Related inject ID."},
                },
                "required": ["action"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="manage_openaev_vulnerability",
            description=(
                "Manage OpenAEV vulnerabilities. Actions: 'list' (search), 'get' "
                "(vulnerability_id), 'create' (external_id e.g. CVE-2024-0001 + cvss "
                "0.0-10.0, optional description and config JSON for extra fields like "
                "vulnerability_remediation/vulnerability_reference_urls), 'update' "
                "(vulnerability_id + fields), 'delete' (vulnerability_id). Use the core "
                "get_openaev_vulnerability tool to look up by CVE."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "get", "create", "update", "delete"]},
                    "vulnerability_id": {"type": "string"},
                    "external_id": {"type": "string", "description": "CVE identifier."},
                    "cvss": {"type": "number", "description": "CVSS v3.1 score 0.0-10.0."},
                    "description": {"type": "string"},
                    "config": {"type": "string", "description": "JSON object of extra vulnerability fields."},
                    "search": {"type": "string"},
                    "limit": {"type": "integer", "default": 20},
                },
                "required": ["action"],
            },
            source=_SOURCE,
        ),
    ]
