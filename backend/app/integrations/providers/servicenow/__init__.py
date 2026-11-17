"""ServiceNow integration provider package.

Comprehensive, agentic management of ServiceNow ITSM + CMDB + Service Catalog via
the platform's uniform REST surface (Table API, Aggregate API, Attachment API,
Service Catalog API):

* ``incidents``  - incident lifecycle (list/search, get, create, update, work
                   notes/comments, resolve, close).
* ``changes``    - change requests (list/get/create/update + state transitions).
* ``problems``   - problem records (list/get/create/update).
* ``tasks``      - the unified "my work queue" across every task type.
* ``cmdb``       - configuration items + relationships (search/get/walk).
* ``users``      - users & groups + group membership (sys_id resolution).
* ``knowledge``  - knowledge base article search + read.
* ``catalog``    - Service Catalog browse, order (order_now), and request tracking.
* ``records``    - generic Table API escape hatch (query/get/count/manage any
                   table) + attachments (list/upload/download).

Authentication: HTTP Basic (username + password, ideally a dedicated integration
user). ``integration_tools._get_token`` assembles the compound
``base_url||username||password`` token this package parses. The instance
``base_url`` (``https://INSTANCE.service-now.com``) is also the web UI host, so
record deep links are surfaced to the agent.
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.egress import egress_client
from app.integrations.base import BaseIntegration, OAuthDefaults
from app.tools.base import CallableToolExecutor, ToolDefinition

from . import (
    catalog,
    changes,
    cmdb,
    incidents,
    knowledge,
    problems,
    records,
    tasks,
    users,
)
from ._common import (
    _SOURCE,
    build_servicenow_headers,
    parse_servicenow_token,
    servicenow_api_base,
    sn_request,
    table_url,
)

logger = logging.getLogger(__name__)

__all__ = [
    "ServiceNowIntegration",
    "build_servicenow_headers",
    "build_tools",
    "servicenow_api_base",
]


class ServiceNowIntegration(BaseIntegration):
    provider_name = "servicenow"
    display_name = "ServiceNow"
    icon = "Workflow"
    description = (
        "Connect ServiceNow for ITSM and operations: triage and update incidents, "
        "changes, and problems; manage your task queue; search the CMDB and "
        "knowledge base; order Service Catalog items; and reach any table via the "
        "Table API."
    )
    auth_types = ["api_key"]

    def get_oauth_defaults(self) -> OAuthDefaults | None:
        return None

    async def validate_connection(self, token: str) -> bool:
        base, username, password = parse_servicenow_token(token)
        if not base or not username:
            return False
        headers = build_servicenow_headers(username, password)
        resp, err = await sn_request(
            "get",
            table_url(base, "sys_user"),
            "validate connection",
            headers=headers,
            params={"sysparm_limit": 1, "sysparm_fields": "sys_id"},
            timeout=15,
        )
        return err is None and resp is not None and resp.status_code == 200

    async def get_user_info(self, token: str) -> dict:
        base, username, password = parse_servicenow_token(token)
        if not base or not username:
            return {}
        headers = build_servicenow_headers(username, password)
        try:
            async with egress_client(timeout=15) as client:
                resp = await client.get(
                    table_url(base, "sys_user"),
                    headers=headers,
                    params={
                        "sysparm_query": f"user_name={username}",
                        "sysparm_limit": 1,
                        "sysparm_fields": "name,email,user_name,sys_id",
                        "sysparm_display_value": "true",
                    },
                )
                resp.raise_for_status()
                rows = (resp.json() or {}).get("result") or []
        except Exception:
            return {"instance": base, "user_name": username}
        record = rows[0] if rows else {}
        return {
            "instance": base,
            "name": record.get("name") or username,
            "email": record.get("email"),
            "user_name": record.get("user_name") or username,
            "sys_id": record.get("sys_id"),
        }


def build_tools(
    token: str, executor: CallableToolExecutor, **kwargs: Any
) -> list[ToolDefinition]:
    """Build the full ServiceNow tool surface from ``base_url||username||password``.

    Returns no tools when the instance base URL is missing (the integration must
    be configured with the instance URL). Credential problems surface at call
    time as a clear authentication error from ServiceNow.
    """
    base, username, password = parse_servicenow_token(token)
    if not servicenow_api_base(base):
        logger.debug(
            "ServiceNow base URL is empty - returning no tools. Configure the "
            "instance URL (https://INSTANCE.service-now.com)."
        )
        return []
    headers = build_servicenow_headers(username, password)
    persist_file_fn = kwargs.get("persist_file_fn")

    async def get_servicenow_status() -> str:
        """Validate the ServiceNow connection (cheap authenticated probe)."""
        _resp, err = await sn_request(
            "get",
            table_url(base, "sys_user"),
            "verify connection",
            headers=headers,
            params={"sysparm_limit": 1, "sysparm_fields": "sys_id"},
        )
        if err:
            return err
        return f"Connected to ServiceNow at {base} (credentials valid)."

    executor.register("get_servicenow_status", get_servicenow_status)

    tools: list[ToolDefinition] = [
        ToolDefinition(
            name="get_servicenow_status",
            description=(
                "Validate the ServiceNow connection (cheap authenticated probe). "
                "Use first if other ServiceNow tools fail."
            ),
            input_schema={"type": "object", "properties": {}, "required": []},
            source=_SOURCE,
            priority=2,
        ),
    ]
    tools += incidents.register(base, headers, executor)
    tools += changes.register(base, headers, executor)
    tools += problems.register(base, headers, executor)
    tools += tasks.register(base, headers, executor)
    tools += cmdb.register(base, headers, executor)
    tools += users.register(base, headers, executor)
    tools += knowledge.register(base, headers, executor)
    tools += catalog.register(base, headers, executor)
    tools += records.register(
        base, headers, executor, persist_file_fn=persist_file_fn
    )
    return tools
