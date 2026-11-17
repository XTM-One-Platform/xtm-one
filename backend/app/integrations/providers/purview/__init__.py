"""Microsoft Purview integration provider package.

Best-in-class building block for an agentic data-governance, risk, and
compliance team. Microsoft Purview spans two product pillars, and this provider
covers both from a single Azure AD app registration:

* **Risk & Compliance** (Microsoft Graph ``security`` namespace):
  - ``ediscovery``             - eDiscovery (Premium) cases, custodians,
    searches, legal holds, review sets, operations.
  - ``audit``                  - unified audit log search (async query API).
  - ``information_protection`` - sensitivity labels.
  - ``records_management``     - retention labels + event-based retention.
  - ``privacy``                - Subject Rights Requests (GDPR / DSR).
* **Unified Data Governance** (Purview Data Map / Apache Atlas API):
  - ``catalog``                - catalog search, assets, lineage, glossary,
    collections.

Authentication (resolved by ``integration_tools._get_token``):

* **Delegated OAuth** (default) - the tools redeem the integration's refresh
  token per Microsoft audience (Graph + Purview Data Map), exactly like
  Microsoft Defender XDR. ``build_tools`` receives a ``token_acquirer``.
* **App-only client credentials** - ``token`` is the compound
  ``tenant_id||client_id||client_secret||account_name`` and each audience token
  is minted via the client-credentials grant. Recommended for unattended
  governance / compliance automation.

The Data Map (catalog) tools additionally require a Purview governance
``account_name`` and a data-plane role on the principal; the compliance tools
need only the Graph permissions.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from app.integrations.base import BaseIntegration, OAuthDefaults
from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import PURVIEW_SCOPE, PurviewClient, TokenAcquirer, get_app_graph_token
from .audit import register as register_audit
from .catalog import register as register_catalog
from .ediscovery import register as register_ediscovery
from .information_protection import register as register_information_protection
from .privacy import register as register_privacy
from .records_management import register as register_records_management

__all__ = [
    "PURVIEW_SCOPE",
    "PurviewClient",
    "PurviewIntegration",
    "build_tools",
    "get_app_graph_token",
]

RefreshFn = Callable[[], Awaitable[str | None]]

# Delegated OAuth scopes - Microsoft Graph (the compliance pillar) is the single
# authorize-time resource. The Purview Data Map audience
# (https://purview.azure.net) is acquired at runtime by redeeming the refresh
# token, so its scope must NOT be mixed in here (Azure AD v2 rejects
# multi-resource token requests) - the app registration carries that delegated
# permission and admin consent instead (see the catalog setup steps).
_SCOPES = [
    "openid",
    "profile",
    "email",
    "offline_access",
    "User.Read",
    "eDiscovery.Read.All",
    "eDiscovery.ReadWrite.All",
    "AuditLogsQuery.Read.All",
    "SensitivityLabels.Read.All",
    "RecordsManagement.Read.All",
    "RecordsManagement.ReadWrite.All",
    "SubjectRightsRequest.Read.All",
    "SubjectRightsRequest.ReadWrite.All",
]


class PurviewIntegration(BaseIntegration):
    provider_name = "purview"
    display_name = "Microsoft Purview"
    icon = "ShieldCheck"
    description = (
        "Connect Microsoft Purview for data governance, risk & compliance: "
        "eDiscovery, unified audit log search, sensitivity & retention labels, "
        "subject rights requests, and the data catalog (search, assets, "
        "lineage, glossary)."
    )
    auth_types = ["oauth", "api_key"]

    def get_oauth_defaults(self) -> OAuthDefaults:
        return OAuthDefaults(
            authorize_url="https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
            token_url="https://login.microsoftonline.com/common/oauth2/v2.0/token",  # noqa: S106
            scopes=_SCOPES,
        )

    async def validate_connection(self, token: str) -> bool:
        client = PurviewClient(token)
        if client.app_only:
            # App-only: success == we can mint a Microsoft Graph token (the
            # connectivity-test endpoint validates this path with a real mint).
            return (await client._graph_token()) is not None
        # Delegated: the raw token is a Microsoft Graph access token; /me works
        # for any signed-in user (User.Read).
        _, err = await client.graph_get_json("/me", "validate connection")
        return err is None

    async def get_user_info(self, token: str) -> dict:
        client = PurviewClient(token)
        if client.app_only:
            return {}
        data, _ = await client.graph_get_json("/me", "get user info")
        return data or {}


def build_tools(
    token: str,
    executor: CallableToolExecutor,
    *,
    refresh_fn: RefreshFn | None = None,
    token_acquirer: TokenAcquirer | None = None,
    account_name: str = "",
    **_kwargs: Any,
) -> list[ToolDefinition]:
    """Build the Microsoft Purview tool set.

    * **Delegated OAuth**: ``token_acquirer`` is provided; the client mints a
      per-audience token (Graph + Purview Data Map) by redeeming the refresh
      token.
    * **App-only**: ``token`` is ``tenant_id||client_id||client_secret||account``
      and the client mints its own tokens via the client-credentials grant.

    ``account_name`` (the Purview governance account) enables the Data Map
    (catalog) tools; without it those tools return an actionable error.
    """
    client = PurviewClient(
        token, token_acquirer=token_acquirer, account_name=account_name
    )

    tools: list[ToolDefinition] = []
    tools += register_ediscovery(client, executor)
    tools += register_audit(client, executor)
    tools += register_information_protection(client, executor)
    tools += register_records_management(client, executor)
    tools += register_privacy(client, executor)
    tools += register_catalog(client, executor)
    return tools
