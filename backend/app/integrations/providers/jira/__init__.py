"""Atlassian Jira integration provider package.

Comprehensive management of Jira Cloud across its three REST APIs:

* Platform v3 (``/rest/api/3``)        - issues, fields, comments/worklogs,
                                          attachments, projects/versions/
                                          components, metadata, users/groups,
                                          links, filters/dashboards.
* Software/Agile (``/rest/agile/1.0``) - boards, sprints, backlog, epics.
* Service Management (``/rest/servicedeskapi``) - service desks, requests,
                                          queues, SLA, approvals, participants,
                                          organizations, knowledge base.

The provider is split by domain (mirroring the ``opencti`` package):

* ``_common``             - credential parsing, HTTP wrapper, ADF + issue +
                            custom-field formatters, shared constants.
* ``issues``              - JQL search, issue CRUD, transitions, assignment,
                            history, watchers (custom-field aware).
* ``fields``              - field discovery (name -> customfield id),
                            create/edit field metadata.
* ``comments``            - comments + worklogs.
* ``attachments``         - list / download / add / delete attachments.
* ``agile``               - boards, sprints, backlog, epics.
* ``projects``            - projects, versions, components.
* ``metadata``            - issue types, priorities, statuses, resolutions,
                            link types, labels.
* ``users``               - myself, user search, assignable users, groups.
* ``links``               - issue links + remote (web) links.
* ``filters``             - saved filters + dashboards.
* ``service_management``  - Jira Service Management.

Token format passed by the dispatcher: ``cloud_id||access_token`` (the
``extra_config`` cloud_id is prepended by ``_get_token`` in
``integration_tools.py``).
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.egress import egress_client
from app.integrations.base import BaseIntegration, OAuthDefaults
from app.tools.base import CallableToolExecutor, ToolDefinition

from . import (
    agile,
    attachments,
    comments,
    fields,
    filters,
    issues,
    links,
    metadata,
    projects,
    service_management,
    users,
)
from ._common import (
    _BASE_URL,
    format_issue,
    jira_headers,
    make_refreshable_headers,
    parse_jira_token,
)

logger = logging.getLogger(__name__)

__all__ = ["JiraIntegration", "build_tools", "format_issue"]


class JiraIntegration(BaseIntegration):
    provider_name = "jira"
    display_name = "Jira"
    icon = "ClipboardList"
    description = (
        "Connect Atlassian Jira for project management, issue tracking (with full "
        "custom-field support), agile boards, sprint planning, service desk "
        "management, and team collaboration."
    )
    auth_types = ["oauth"]

    def get_oauth_defaults(self) -> OAuthDefaults:
        return OAuthDefaults(
            authorize_url="https://auth.atlassian.com/authorize",
            token_url="https://auth.atlassian.com/oauth/token",  # noqa: S106
            # OAuth token exchange endpoint URL, not a secret.
            scopes=[
                "read:me",
                "read:jira-work",
                "write:jira-work",
                "read:jira-user",
                "manage:jira-project",
                "manage:jira-configuration",
                "manage:jira-webhook",
                "manage:jira-data-provider",
                "read:servicedesk-request",
                "write:servicedesk-request",
                "manage:servicedesk-customer",
                "offline_access",
            ],
        )

    async def validate_connection(self, token: str) -> bool:
        async with egress_client(timeout=15) as client:
            resp = await client.get(
                f"{_BASE_URL}/me",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
            return resp.status_code == 200

    async def get_user_info(self, token: str) -> dict:
        async with egress_client(timeout=15) as client:
            resp = await client.get(
                f"{_BASE_URL}/me",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()
            return {
                "account_id": data.get("account_id"),
                "name": data.get("name"),
                "email": data.get("email"),
                "picture": data.get("picture"),
            }


def build_tools(
    token: str, executor: CallableToolExecutor, **kwargs: Any
) -> list[ToolDefinition]:
    """Build the full Jira tool surface.

    ``token`` is ``cloud_id||access_token`` (compound). Returns no tools when no
    cloud_id is present (the integration must be re-authorized to resolve the
    Atlassian site).
    """
    base, access_token = parse_jira_token(token)
    if not base or not access_token:
        logger.debug(
            "Jira cloud_id or access token is empty - returning no tools. "
            "Re-authorize the integration so the accessible Atlassian site and "
            "a valid token can be resolved."
        )
        return []
    headers = jira_headers(access_token)
    # Atlassian access tokens expire after ~1 h. Carry the dispatcher's
    # refresh callback on the shared headers dict so jira_request can
    # auto-refresh on 401/403 instead of failing with "re-authorize".
    refresh_fn = kwargs.get("refresh_fn")
    if refresh_fn is not None:

        async def _refresh_compound() -> str | None:
            # The dispatcher's refresh_fn returns the NEW compound token
            # (cloud_id||access_token for Jira) or a raw token depending on
            # provider wiring — extract the bearer part either way.
            new_token = await refresh_fn()
            if not new_token:
                return None
            _, new_access = parse_jira_token(new_token)
            return new_access or new_token

        headers = make_refreshable_headers(headers, _refresh_compound)
    persist_file_fn = kwargs.get("persist_file_fn")

    tools: list[ToolDefinition] = []
    tools += issues.register(base, headers, executor)
    tools += fields.register(base, headers, executor)
    tools += comments.register(base, headers, executor)
    tools += attachments.register(
        base, headers, executor, persist_file_fn=persist_file_fn
    )
    tools += agile.register(base, headers, executor)
    tools += projects.register(base, headers, executor)
    tools += metadata.register(base, headers, executor)
    tools += users.register(base, headers, executor)
    tools += links.register(base, headers, executor)
    tools += filters.register(base, headers, executor)
    tools += service_management.register(base, headers, executor)
    return tools
